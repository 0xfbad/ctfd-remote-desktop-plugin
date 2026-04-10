import logging
from threading import Lock
from collections import defaultdict
from .event_logger import event_logger

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, host_manager):
        self.host_manager = host_manager
        self.container_counts = defaultdict(int)  # {context_name: count}
        self.health = {}  # {context_name: healthy}
        self.weights = {}  # {context_name: weight}
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

        # health-check each context outside the lock (network I/O)
        new_health = {}
        new_weights = {}
        events = []
        for ctx in contexts:
            name = ctx.context_name
            is_connected = name in connected

            if is_connected:
                has_image = self.host_manager.check_image(name, docker_image)
                image_info = self.host_manager.get_image_info(name, docker_image) if has_image else None
            else:
                has_image = False
                image_info = None

            healthy = is_connected and has_image
            new_health[name] = healthy
            new_weights[name] = ctx.weight

            if healthy:
                meta = {"context_name": name}
                if image_info:
                    meta["image"] = image_info
                events.append(("host_healthy", f"context {name} is healthy", "info", meta))
            else:
                reason = "connection failed" if not is_connected else "image not found"
                events.append(
                    (
                        "host_unhealthy",
                        f"context {name} marked unhealthy: {reason}",
                        "warning",
                        {"context_name": name, "reason": reason},
                    )
                )

        known = {ctx.context_name for ctx in contexts}

        with self.lock:
            self.health = new_health
            self.weights = new_weights
            for name in list(self.container_counts.keys()):
                if name not in known:
                    del self.container_counts[name]
            for name in known:
                if name not in self.container_counts:
                    self.container_counts[name] = 0

        for event_type, message, level, metadata in events:
            event_logger.log_event(event_type, message, level=level, metadata=metadata)

        healthy_count = sum(1 for h in new_health.values() if h)
        logger.info(f"loaded {len(contexts)} contexts, {healthy_count} healthy")

    def has_healthy_context(self):
        with self.lock:
            return any(self.health.values())

    def _pick_best_context(self):
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

    def select_and_reserve(self):
        with self.lock:
            name = self._pick_best_context()
            self.container_counts[name] += 1
            logger.debug(f"select_and_reserve: {name}, now {self.container_counts[name]}")
            return name

    def reserve_slot(self, context_name):
        with self.lock:
            self.container_counts[context_name] += 1
            logger.debug(f"reserved slot on {context_name}, now {self.container_counts[context_name]}")

    def release_slot(self, context_name):
        with self.lock:
            if self.container_counts[context_name] > 0:
                self.container_counts[context_name] -= 1
                logger.debug(f"released slot on {context_name}, now {self.container_counts[context_name]}")

    def mark_unhealthy(self, context_name, reason="unreachable"):
        with self.lock:
            self.health[context_name] = False
            logger.warning(f"context {context_name} marked unhealthy: {reason}")
            event_logger.log_event(
                "host_unhealthy",
                f"context {context_name} marked unhealthy: {reason}",
                level="warning",
                metadata={"context_name": context_name, "reason": reason},
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

    def health_check(self):
        with self.lock:
            names = list(self.health.keys())

        for name in names:
            reachable = self.host_manager.ping(name)
            with self.lock:
                was_healthy = self.health.get(name)

            if reachable and not was_healthy:
                self.mark_healthy(name)
                logger.info(f"health_check: context {name} recovered")
            elif not reachable and was_healthy:
                self.mark_unhealthy(name)
                logger.warning(f"health_check: context {name} unreachable")
