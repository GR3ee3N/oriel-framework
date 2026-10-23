"""Example app with an add command and an add-on-handled Info key.

The page changes the count through add; Info resets it without reaching the page.
"""
import oriel


class Hello(oriel.App):
    def __init__(self):
        super().__init__()
        self.count = 0

    def state(self):
        return {**super().state(), "count": self.count}

    @oriel.command("add")
    def add(self, amount):
        if not isinstance(amount, int):
            raise oriel.CommandError("amount must be a whole number", "invalid")
        self.count += amount
        self.publish()
        return self.count

    def on_key(self, key):
        if key != "info":
            return False
        self.count = 0
        self.publish()
        return True


oriel.run(Hello)
