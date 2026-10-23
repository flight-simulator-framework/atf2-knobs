"""Evaluation-budget progress, independent of machine reads and GUI state."""


class BudgetProgress:
    def __init__(self, budgets):
        self.budgets = {str(key): max(1, int(value)) for key, value in budgets.items()}
        self.used = dict.fromkeys(self.budgets, 0)

    def update(self, key, index=0, *, done=False):
        if key in self.budgets:
            budget = self.budgets[key]
            self.used[key] = budget if done else max(self.used[key], min(budget, max(0, int(index or 0))))
        return self.snapshot()

    def finish(self):
        self.used = dict(self.budgets)
        return self.snapshot()

    def snapshot(self):
        total = sum(self.budgets.values())
        completed = sum(self.used.values())
        return {"completed": completed, "total": total,
                "percent": 100.0 * completed / total if total else 0.0}
