class RemoteDesktopException(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.message = str(args[0]) if args else "unknown error"

    def __str__(self) -> str:
        return self.message


# routes map this to 503, every other plugin error maps to 500
class HostsUnavailableException(RemoteDesktopException):
    pass


CAPACITY_MESSAGE = "All servers are at capacity right now. Please try again in a few minutes."


class HostsAtCapacityException(HostsUnavailableException):
    pass
