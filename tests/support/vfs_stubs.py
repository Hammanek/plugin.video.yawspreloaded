"""In-memory Kodi VFS double with real write/close/rename/delete semantics."""


class RecordingVFS:
    def __init__(self, folders=()):
        self.files = {}
        self.folders = set(folders)
        self.open_files = []
        self.renames = []
        self.deleted = []
        self.fail_writes = False
        self.fail_renames = False

    def exists(self, path):
        return path in self.folders or path in self.files

    def File(self, path, mode="r"):
        output = VFSFile(self, path)
        self.open_files.append(output)
        return output

    def rename(self, source, destination):
        self.renames.append((source, destination))
        if self.fail_renames or source not in self.files:
            return False
        self.files[destination] = self.files.pop(source)
        return True

    def delete(self, path):
        self.deleted.append(path)
        if path not in self.files:
            return False
        del self.files[path]
        return True


class VFSFile:
    def __init__(self, vfs, path):
        self.vfs = vfs
        self.path = path
        self.closed = False
        self.writes = []
        self.vfs.files.setdefault(path, b"")

    def write(self, data):
        self.writes.append(bytes(data))
        if self.vfs.fail_writes:
            return False
        self.vfs.files[self.path] += bytes(data)
        return True

    def close(self):
        self.closed = True
