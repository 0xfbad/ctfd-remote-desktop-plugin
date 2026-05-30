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
