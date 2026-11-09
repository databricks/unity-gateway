"""`python -m ucode`: the same entry point as the `ug` console script.

The background token refresher runs this way, through ``sys.executable``, because a console-script
path is not reliably re-runnable on every platform.
"""

from ucode.entrypoint import main

if __name__ == "__main__":
    main()
