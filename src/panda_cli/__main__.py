"""Allow `python -m panda_cli` to launch the CLI through the isolated entry point."""

from panda_cli.entry import main

if __name__ == "__main__":
    main()
