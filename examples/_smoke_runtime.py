"""Throwaway smoke test: confirm the service's browser runtime launches and can
dump a public page. No credentials, no login. Run: python examples/_smoke_runtime.py"""
from accounts_pilot.runtime.browser import BrowserRuntime

with BrowserRuntime(stealth=False, humanize=False) as rt:
    rt.goto("https://join.booking.com/")
    print("URL:", rt.page.url)
    print("dumped:", rt.dump_capture("smoke_public_landing"))
print("RUNTIME OK")
