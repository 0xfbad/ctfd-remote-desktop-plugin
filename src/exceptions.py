class RemoteDesktopException(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.message = str(args[0]) if args else "unknown error"

    def __str__(self) -> str:
        return self.message


# raised when no healthy docker context is available. routes map this to 503,
# distinct from generic RemoteDesktopException 500
class HostsUnavailableException(RemoteDesktopException):
    pass


CAPACITY_MESSAGE = "All servers are at capacity right now. Please try again in a few minutes."


# raised when every healthy context is at its max_containers cap. subclass of
# HostsUnavailableException so existing route handling maps it to 503
class HostsAtCapacityException(HostsUnavailableException):
    pass
