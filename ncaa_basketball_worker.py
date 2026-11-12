# /// script
# requires-python = ">=3.13"
# dependencies = ["vgi-python[http]>=0.51.0,<0.52", "httpx>=0.27,<1", "pyarrow>=18,<26"]
# ///
"""Stdio entry point for attaching the NCAA basketball VGI catalog."""

from vgi_ncaa_basketball.worker import main

if __name__ == "__main__":
    main()
