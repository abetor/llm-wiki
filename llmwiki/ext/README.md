Extensions live in separate directories with their own README files. An extension imports the
core, while the core remains unaware of extensions except for subcommand registration in cli.py.
