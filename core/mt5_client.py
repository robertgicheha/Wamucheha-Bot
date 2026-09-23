"""
MetaTrader5 module loader.

The official MetaTrader5 package only runs on Windows. On the Linux VPS the
terminal runs under Wine in the `mt5` container (lprett/mt5linux image), which
serves the real MetaTrader5 module over RPyC. Set MT5_RPC_HOST (and optionally
MT5_RPC_PORT, default 18812) to use it; leave it unset on Windows to import the
package directly.

get_mt5() returns an object with the same surface as `import MetaTrader5 as mt5`
(functions + constants). Remote results are copied back as plain values:
namedtuples become attribute objects, rate arrays become lists of dicts.
"""
import os
import threading
from types import SimpleNamespace

_remote = None
_lock = threading.Lock()


def get_mt5():
    host = os.environ.get("MT5_RPC_HOST", "").strip()
    if not host:
        import MetaTrader5 as mt5
        return mt5
    global _remote
    with _lock:
        if _remote is None:
            _remote = RemoteMT5(host, int(os.environ.get("MT5_RPC_PORT", "18812")))
        return _remote


# Runs inside the Wine Python: converts MT5 results to picklable builtins.
_SERVER_HELPERS = r'''
import sys
sys.path.append('C:\\mt5libs')
import MetaTrader5 as mt5

def _plain(o):
    if hasattr(o, "item") and getattr(o, "shape", None) == ():
        return o.item()  # numpy scalar
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if hasattr(o, "_asdict"):
        return {"__obj__": {k: _plain(v) for k, v in o._asdict().items()}}
    if hasattr(o, "dtype") and getattr(o.dtype, "names", None):
        names = list(o.dtype.names)
        return {"__rows__": [dict(zip(names, row)) for row in o.tolist()]}
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, dict):
        return {k: _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    return str(o)
'''


def _builtin(o):
    """Numpy scalars repr as np.float64(...), which the remote eval can't parse."""
    if hasattr(o, "item") and getattr(o, "shape", None) == ():
        return o.item()
    if isinstance(o, dict):
        return {k: _builtin(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return type(o)(_builtin(v) for v in o)
    return o


def _rebuild(o):
    if isinstance(o, dict):
        if "__obj__" in o:
            d = {k: _rebuild(v) for k, v in o["__obj__"].items()}
            ns = SimpleNamespace(**d)
            ns._asdict = lambda d=d: dict(d)
            return ns
        if "__rows__" in o:
            return o["__rows__"]
        return {k: _rebuild(v) for k, v in o.items()}
    if isinstance(o, list):
        return tuple(_rebuild(v) for v in o)
    return o


class RemoteMT5:
    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port
        self._conn = None
        self._constants = {}
        self._call_lock = threading.Lock()

    def _connect(self):
        import rpyc
        conn = rpyc.classic.connect(self._host, self._port)
        conn._config["sync_request_timeout"] = 120
        conn.execute(_SERVER_HELPERS)
        self._conn = conn

    def _eval(self, code: str):
        import rpyc
        with self._call_lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._connect()
                    return rpyc.classic.obtain(self._conn.eval(code))
                except (EOFError, ConnectionError, OSError):
                    # Container restarted — reconnect once, then give up.
                    self._conn = None
                    if attempt == 2:
                        raise

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name.isupper():
            if name not in self._constants:
                self._constants[name] = self._eval(f"mt5.{name}")
            return self._constants[name]

        def call(*args, **kwargs):
            args, kwargs = _builtin(args), _builtin(kwargs)
            return _rebuild(self._eval(f"_plain(mt5.{name}(*{args!r}, **{kwargs!r}))"))
        return call
