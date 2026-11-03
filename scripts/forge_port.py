"""The forge failure boundary, shared by the adapter and its Use Cases callers."""


class ForgeError(RuntimeError):
    """The forge could not verify or write the requested state."""
