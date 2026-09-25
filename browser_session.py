"""Per-session command budgets, including the first NEW_SESSION request."""

from selenium import webdriver
from selenium.webdriver.remote.command import Command

NAVIGATION_TIMEOUT = 30.0
STARTUP_TIMEOUT = 30.0
COMMAND_TIMEOUT = 10.0


class BoundedCommands:
    def execute(self, driver_command, params=None):
        timeout = {
            Command.NEW_SESSION: STARTUP_TIMEOUT,
            Command.GET: NAVIGATION_TIMEOUT + 5.0,
            Command.QUIT: 5.0,
        }.get(driver_command, COMMAND_TIMEOUT)
        self.command_executor.client_config.timeout = timeout
        # Do not multiply the deadline by retrying an unresponsive local driver.
        pool = getattr(self.command_executor, "_conn", None)
        if pool is not None:
            pool.connection_pool_kw["retries"] = 0
        return super().execute(driver_command, params)


class CaptureChrome(BoundedCommands, webdriver.Chrome):
    pass


class CaptureEdge(BoundedCommands, webdriver.Edge):
    pass


class CaptureFirefox(BoundedCommands, webdriver.Firefox):
    pass
