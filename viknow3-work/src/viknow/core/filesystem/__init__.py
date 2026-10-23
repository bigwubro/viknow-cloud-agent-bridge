"""Session file management on the shared session path tree."""

__all__ = ["FilesystemManager"]


def __getattr__(name: str):
    if name == "FilesystemManager":
        from viknow.core.filesystem.manager import FilesystemManager

        return FilesystemManager
    raise AttributeError(name)
