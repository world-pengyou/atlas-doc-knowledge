class DocumentNotFound(ValueError):
    pass


class DocumentConflict(ValueError):
    pass


class StoragePathError(ValueError):
    pass


class StorageCheckError(Exception):
    pass
