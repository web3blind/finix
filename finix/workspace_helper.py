"""Removed Workspace-skill authorization bridge. Existing files are untouched."""

def main():
    raise RuntimeError("WORKSPACE_MIGRATION_REQUIRED: use hermes auth add finix:google; legacy grants are not adopted")

if __name__ == "__main__":
    main()
