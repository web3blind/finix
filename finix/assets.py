"""Locate a public offline asset in the matching native or wheel installation."""
from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path

from . import core


def key_page():
    module = Path(__file__).resolve()
    native = module.parent.parent / 'dist' / 'keygen.html'
    if native.is_file():
        return native
    try:
        package = distribution('finix')
    except PackageNotFoundError:
        raise core.FinixError('Offline key page is not installed') from None
    if Path(str(package.locate_file('finix/assets.py'))).resolve() != module:
        raise core.FinixError('Offline key page belongs to another installation')
    for entry in package.files or ():
        if entry.parts[-4:] == ('share', 'finix', 'dist', 'keygen.html'):
            asset = Path(str(package.locate_file(entry)))
            if asset.is_file():
                return asset
    raise core.FinixError('Offline key page is not installed')
