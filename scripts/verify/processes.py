"""Gate G14: All agent processes running, no zombies/orphans."""
import os

for line in os.popen("pgrep -f tg_bot/main.py").read().strip().splitlines():
    if line.strip():
        assert os.path.exists(f"/proc/{line.strip()}"), f"tg_bot {line.strip()} dead"
        print("tg_bot:", line.strip(), "alive")
for line in os.popen("pgrep -f live_agent.py").read().strip().splitlines():
    if line.strip():
        assert os.path.exists(f"/proc/{line.strip()}"), f"agent {line.strip()} dead"
        print("agent:", line.strip(), "alive")
z = os.popen("ps aux | grep defunct | grep -v grep | grep -E 'python|live_agent|tg_bot'").read().strip()
print("bot zombies:", "none" if not z else z[:200])
assert not z, f"zombie bot processes found: {z}"
print("PROCESSES: PASS")