import time
from panda_core.redact import redact_text
for n in (25_000, 50_000, 100_000, 200_000):
    t="secret"*n+":"; s=time.perf_counter(); redact_text(t); print(n*6, round(time.perf_counter()-s,2))
t=("a"*60+"_")*20000+":"; s=time.perf_counter(); redact_text(t); print("benign 1.2MB", round(time.perf_counter()-s,2))
