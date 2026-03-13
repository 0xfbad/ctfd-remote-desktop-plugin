import os
import json
import asyncio
import logging

import aiodocker

logger = logging.getLogger(__name__)


def parse_size(s):
    # human-readable size string to bytes, e.g. '4g' becomes 4294967296
    s = str(s).strip().lower()
    multipliers = {"k": 1024, "m": 1024**2, "g": 1024**3, "gb": 1024**3, "mb": 1024**2, "kb": 1024}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(s)


class AsyncBridge:
    def __init__(self):
        self._loop = asyncio.new_event_loop()

        # gevent monkey-patches threading.Thread into greenlets, need the real one
        # so the asyncio loop gets its own OS thread
        try:
            import gevent.monkey

            RealThread = gevent.monkey.get_original("threading", "Thread")
        except Exception:
            from threading import Thread as RealThread

        self._thread = RealThread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def shutdown(self):
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class DockerHostManager:
    def __init__(self):
        self._bridge = AsyncBridge()
        self._clients = {}  # context_name -> aiodocker.Docker
        self._pub_hostnames = {}  # context_name -> pub_hostname

    def _resolve_endpoint(self, context_name, hostname):
        context_file = os.path.expanduser(f"~/.docker/contexts/meta/{context_name}/meta.json")
        if os.path.exists(context_file):
            try:
                with open(context_file, "r") as f:
                    meta = json.load(f)
                    endpoint = meta.get("Endpoints", {}).get("docker", {}).get("Host")
                    if endpoint:
                        return endpoint
            except Exception as e:
                logger.warning(f"could not read context meta for '{context_name}': {e}")

        if hostname:
            if "@" in hostname:
                return f"ssh://{hostname}"
            return f"ssh://root@{hostname}"

        return None

    async def _connect_context(self, context_name, endpoint):
        client = aiodocker.Docker(url=endpoint)
        await client.ping()
        return client

    def load_contexts(self, contexts):
        """(re)connect all enabled contexts. contexts is a list of
        DesktopDockerContextModel rows."""
        self._bridge.run(self._load_contexts_async(contexts))

    async def _load_contexts_async(self, contexts):
        # close old clients first
        for name, client in self._clients.items():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
        self._pub_hostnames.clear()

        for ctx in contexts:
            endpoint = self._resolve_endpoint(ctx.context_name, ctx.hostname)
            if not endpoint:
                logger.warning(f"no endpoint for context '{ctx.context_name}', skipping")
                continue

            try:
                client = await self._connect_context(ctx.context_name, endpoint)
                self._clients[ctx.context_name] = client
                self._pub_hostnames[ctx.context_name] = ctx.pub_hostname
                logger.info(f"connected to context '{ctx.context_name}' at {endpoint}")
            except Exception as e:
                logger.error(f"could not connect to context '{ctx.context_name}': {e}")

    def get_pub_hostname(self, context_name):
        return self._pub_hostnames.get(context_name)

    def get_connected_contexts(self):
        return list(self._clients.keys())

    def ping(self, context_name):
        try:
            self._bridge.run(self._ping_async(context_name))
            return True
        except Exception:
            return False

    async def _ping_async(self, context_name):
        client = self._clients.get(context_name)
        if not client:
            raise Exception(f"no client for context '{context_name}'")
        await client.ping()

    def run_container(self, context_name, image, name, env, ports, shm_size=None, memory=None, nano_cpus=None):
        return self._bridge.run(
            self._run_container_async(context_name, image, name, env, ports, shm_size, memory, nano_cpus)
        )

    async def _run_container_async(self, context_name, image, name, env, ports, shm_size, memory, nano_cpus):
        client = self._clients.get(context_name)
        if not client:
            raise Exception(f"no client for context '{context_name}'")

        exposed_ports = {p: {} for p in ports}
        port_bindings = {p: [{"HostPort": ""}] for p in ports}

        env_list = [f"{k}={v}" for k, v in env.items()]

        host_config = {
            "PortBindings": port_bindings,
            "AutoRemove": True,
        }
        if shm_size:
            host_config["ShmSize"] = shm_size
        if memory:
            host_config["Memory"] = memory
        if nano_cpus:
            host_config["NanoCpus"] = nano_cpus

        config = {
            "Image": image,
            "Env": env_list,
            "ExposedPorts": exposed_ports,
            "HostConfig": host_config,
        }

        container = await client.containers.create_or_replace(name=name, config=config)
        await container.start()

        # poll for port mappings (container might take a moment to bind)
        port_map = {}
        for attempt in range(5):
            info = await container.show()
            network_ports = info.get("NetworkSettings", {}).get("Ports", {})

            all_mapped = True
            for p in ports:
                bindings = network_ports.get(p)
                if bindings and len(bindings) > 0:
                    port_map[p] = int(bindings[0]["HostPort"])
                else:
                    all_mapped = False

            if all_mapped and port_map:
                break

            if attempt < 4:
                await asyncio.sleep(0.3)

        if not port_map:
            raise Exception(f"could not get port mappings for {name}")

        return {
            "container_id": container.id,
            "container_name": name,
            "ports": port_map,
        }

    def get_ports(self, context_name, container_id_or_name):
        return self._bridge.run(self._get_ports_async(context_name, container_id_or_name))

    async def _get_ports_async(self, context_name, container_id_or_name):
        client = self._clients.get(context_name)
        if not client:
            raise Exception(f"no client for context '{context_name}'")

        container = await client.containers.get(container_id_or_name)
        info = await container.show()
        network_ports = info.get("NetworkSettings", {}).get("Ports", {})

        port_map = {}
        for port_key, bindings in network_ports.items():
            if bindings and len(bindings) > 0:
                port_map[port_key] = int(bindings[0]["HostPort"])
        return port_map

    def stop_container(self, context_name, container_name, timeout=10):
        try:
            self._bridge.run(self._stop_container_async(context_name, container_name, timeout))
        except Exception as e:
            logger.error(f"error stopping container {container_name}: {e}")

    async def _stop_container_async(self, context_name, container_name, timeout):
        client = self._clients.get(context_name)
        if not client:
            raise Exception(f"no client for context '{context_name}'")

        try:
            container = await client.containers.get(container_name)
            await container.stop(t=timeout)
        except aiodocker.exceptions.DockerError as e:
            # 404 means container already gone (auto-removed), not an error
            if e.status == 404:
                logger.debug(f"container {container_name} already removed")
            else:
                raise

    def check_image(self, context_name, image):
        try:
            self._bridge.run(self._check_image_async(context_name, image))
            return True
        except Exception:
            return False

    async def _check_image_async(self, context_name, image):
        client = self._clients.get(context_name)
        if not client:
            raise Exception(f"no client for context '{context_name}'")
        await client.images.inspect(image)

    def list_images(self, context_name):
        return self._bridge.run(self._list_images_async(context_name))

    async def _list_images_async(self, context_name):
        client = self._clients.get(context_name)
        if not client:
            raise Exception(f"no client for context '{context_name}'")
        images = await client.images.list()
        return images

    def list_containers(self, context_name, name_prefix):
        return self._bridge.run(self._list_containers_async(context_name, name_prefix))

    async def _list_containers_async(self, context_name, name_prefix):
        client = self._clients.get(context_name)
        if not client:
            raise Exception(f"no client for context '{context_name}'")

        filters = {"name": [name_prefix]}
        containers = await client.containers.list(all=True, filters=filters)
        return [c["Id"] for c in containers]

    def close(self):
        try:
            self._bridge.run(self._close_async())
        except Exception:
            pass
        self._bridge.shutdown()

    async def _close_async(self):
        for name, client in self._clients.items():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()
