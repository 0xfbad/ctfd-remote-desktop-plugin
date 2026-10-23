class RemoteDesktopException(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.message = str(args[0]) if args else "unknown error"

    def __str__(self) -> str:
        return self.message


class HostsUnavailableException(RemoteDesktopException):
    pass


class HostsAtCapacityException(HostsUnavailableException):
    pass
