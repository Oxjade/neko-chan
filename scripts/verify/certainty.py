"""Gate G15: Final certainty score report."""
gates_passed = 0
gates_total = 0
for line in open(".unlazy/production-verification/GATES.md"):
    s = line.strip()
    if s.startswith("- [ ]"):
        gates_total += 1
    if s.startswith("- [x]"):
        gates_passed += 1
print(f"gates: {gates_passed} / {gates_total}")
print(f"certainty: {round(gates_passed / max(gates_total, 1) * 100, 1)} %")