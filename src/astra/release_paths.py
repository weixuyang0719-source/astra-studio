"""Separate immutable app code from the existing writable Astra workspace."""
import os
from pathlib import Path

def data_root():
    root = os.environ.get('ASTRA_RELEASE_DATA_ROOT')
    return Path(root).resolve(strict=True) if root else Path(__file__).resolve().parents[2]


def project_output_root():
    """Return the configured project library, without creating or migrating anything."""
    root = os.environ.get('ASTRA_RELEASE_PROJECTS_ROOT')
    return Path(root).expanduser().resolve() if root else data_root()/'outputs'


def runtime_env_root(default=None):
    """Locate installed read-only dependencies separately from candidate data."""
    maintained = os.environ.get('ASTRA_RUNTIME_ENV_ROOT')
    if maintained:
        return Path(maintained).resolve(strict=True)
    return Path(default) if default is not None else data_root()


def project_write_path(path):
    """Keep Formal 2 writes in its own library; shared input assets may be read."""
    result = Path(path).expanduser().resolve()
    if os.environ.get('ASTRA_RELEASE_ISOLATED_PROJECTS') == '1':
        # Resolve junctions as well as symlinks, and fail closed if the configured library was replaced.
        root = project_output_root()
        if root != data_root() / 'projects' or not result.is_relative_to(root):
            number = os.environ.get('ASTRA_RELEASE_NUMBER') or '2'
            raise ValueError(f'新转谱的保存位置须在正式版 {number} 的项目库。已有工程可用「打开项目」选择，源文件不用移动。')
    return result
