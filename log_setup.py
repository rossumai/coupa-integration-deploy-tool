import sys
import os
import datetime

_script_dir = os.path.dirname(os.path.abspath(__file__))
_log_dir = os.path.join(_script_dir, 'logs')
os.makedirs(_log_dir, exist_ok=True)
_log_path = os.path.join(_log_dir, datetime.datetime.now().strftime('run_%Y%m%d_%H%M%S.log'))
_log_file = open(_log_path, 'w', encoding='utf-8')


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()

    def isatty(self):
        return False


sys.stdout = _Tee(sys.__stdout__, _log_file)
sys.stderr = _Tee(sys.__stderr__, _log_file)

print(f"Logging run to: {_log_path}")
