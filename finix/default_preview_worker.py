"""Retired preview worker: no SDK loading, key access, or state adoption."""
def main():
    from finix.recovery_availability import require_public_contract
    require_public_contract()


if __name__ == '__main__':
    main()
