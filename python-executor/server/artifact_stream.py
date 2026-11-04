"""Bounded file snapshot with explicit lifetime, including disconnected clients."""

import codecs
import os


def utf8_end(stream, end):
    start = max(0, end - 4)
    stream.seek(start)
    data = stream.read(end - start)
    while data and data[0] & 0xC0 == 0x80:
        data = data[1:]
    decoder = codecs.getincrementaldecoder("utf-8")()
    decoder.decode(data, final=False)
    return end - len(decoder.getstate()[0])


class ArtifactStream:
    def __init__(self, store, identifier, stream, text=False):
        self.store, self.identifier, self.stream = store, identifier, stream
        self.closed = False
        self.size = os.fstat(stream.fileno()).st_size
        if text:
            self.size = utf8_end(stream, self.size)
        stream.seek(0)
        self.remaining = self.size

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed or not self.remaining:
            self.close()
            raise StopIteration
        try:
            block = self.stream.read(min(self.remaining, 64 * 1024))
            if not block:
                raise OSError("Artifact ended before its declared length.")
            self.remaining -= len(block)
            return block
        except (OSError, ValueError) as exc:
            self.close()
            raise self.store.unavailable(str(exc)) from exc

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.stream.close()
            finally:
                self.store.release_stream(self)
