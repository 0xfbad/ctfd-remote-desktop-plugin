import logging
from threading import Lock
from collections import defaultdict
from .event_logger import event_logger

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, host_manager):
        self.host_manager = host_manager
        self.container_counts = defaultdict(int)  # context_name -> count
        self.health = {}  # context_name -> bool
        self.weights = {}  # context_name -> int
        self.lock = Lock()

    def load_from_db(self):
        from .models import DesktopDockerContextModel, get_setting

        try:
            contexts = DesktopDockerContextModel.query.filter_by(enabled=True).all()
        except Exception as e:
            logger.error(f"could not query docker contexts: {e}")
            contexts = []

        self.host_manager.load_contexts(contexts)
        connected = set(self.host_manager.get_connected_contexts())
        docker_image = get_setting("docker_image")

        with self.lock:
            self.health.clear()
            self.weights.clear()

            for ctx in contexts:
                name = ctx.context_name
                is_connected = name in connected

                if is_connected:
                    has_image = self.host_manager.check_image(name, docker_image)
                else:
                    has_image = False

                healthy = is_connected and has_image
                self.health[name] = healthy
                self.weights[name] = ctx.weight

                if name not in self.container_counts:
                    self.container_counts[name] = 0

                if healthy:
                    event_logger.log_event(
                        "host_healthy", f"context {name} is healthy", level="info", metadata={"context_name": name}
                    )
                else:
                    reason = "connection failed" if not is_connected else "image not found"
                    event_logger.log_event(
                        "host_unhealthy",
                        f"context {name} marked unhealthy: {reason}",
                        level="warning",
                        metadata={"context_name": name, "reason": reason},
                    )

            # prune counts for contexts that no longer exist
            known = {ctx.context_name for ctx in contexts}
            for name in list(self.container_counts.keys()):
                if name not in known:
                    del self.container_counts[name]

        healthy_count = sum(1 for h in self.health.values() if h)
        logger.info(f"loaded {len(contexts)} contexts, {healthy_count} healthy")

    def get_next_context(self):
        with self.lock:
            candidates = []
            for name, healthy in self.health.items():
                if not healthy:
                    continue
                count = self.container_counts[name]
                weight = self.weights.get(name, 1)
                score = weight / (count + 1)
                candidates.append((score, name))

            if not candidates:
                raise Exception("no healthy contexts available")

            candidates.sort(key=lambda x: (-x[0], x[1]))
            return candidates[0][1]

    def reserve_slot(self, context_name):
        with self.lock:
            self.container_counts[context_name] += 1
            logger.debug(f"reserved slot on {context_name}, now {self.container_counts[context_name]}")

    def release_slot(self, context_name):
        with self.lock:
            if self.container_counts[context_name] > 0:
                self.container_counts[context_name] -= 1
                logger.debug(f"released slot on {context_name}, now {self.container_counts[context_name]}")

    def mark_unhealthy(self, context_name):
        with self.lock:
            self.health[context_name] = False
            logger.warning(f"context {context_name} marked unhealthy")
            event_logger.log_event(
                "host_unhealthy",
                f"context {context_name} marked unhealthy",
                level="warning",
                metadata={"context_name": context_name},
            )

    def mark_healthy(self, context_name):
        with self.lock:
            self.health[context_name] = True
            logger.info(f"context {context_name} marked healthy")
            event_logger.log_event(
                "host_healthy",
                f"context {context_name} marked healthy",
                level="info",
                metadata={"context_name": context_name},
            )

    def get_status(self):
        with self.lock:
            status = []
            for name in self.health:
                status.append(
                    {
                        "context_name": name,
                        "pub_hostname": self.host_manager.get_pub_hostname(name),
                        "active_containers": self.container_counts.get(name, 0),
                        "healthy": self.health[name],
                        "weight": self.weights.get(name, 1),
                    }
                )
            return status

    def cleanup(self):
        self.host_manager.close()
        logger.info("orchestrator cleanup completed")
