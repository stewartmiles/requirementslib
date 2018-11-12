# -*- coding=utf-8 -*-
import contextlib
import os
import sys

import attr
import packaging.version
import packaging.specifiers
import packaging.utils

try:
    from setuptools.dist import distutils
except ImportError:
    import distutils

from appdirs import user_cache_dir
from six.moves import configparser
from six.moves.urllib.parse import unquote
from vistir.compat import Path
from vistir.contextmanagers import cd
from vistir.misc import run
from vistir.path import create_tracked_tempdir, ensure_mkdir_p, mkdir_p

from .utils import init_requirement, get_pyproject

try:
    from os import scandir
except ImportError:
    from scandir import scandir


CACHE_DIR = os.environ.get("PIPENV_CACHE_DIR", user_cache_dir("pipenv"))

# The following are necessary for people who like to use "if __name__" conditionals
# in their setup.py scripts
_setup_stop_after = None
_setup_distribution = None


@contextlib.contextmanager
def _suppress_distutils_logs():
    """Hack to hide noise generated by `setup.py develop`.

    There isn't a good way to suppress them now, so let's monky-patch.
    See https://bugs.python.org/issue25392.
    """

    f = distutils.log.Log._log

    def _log(log, level, msg, args):
        if level >= distutils.log.ERROR:
            f(log, level, msg, args)

    distutils.log.Log._log = _log
    yield
    distutils.log.Log._log = f


@ensure_mkdir_p(mode=0o775)
def _get_src_dir():
    src = os.environ.get("PIP_SRC")
    if src:
        return src
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        return os.path.join(virtual_env, "src")
    return os.path.join(os.getcwd(), "src")  # Match pip's behavior.


def _prepare_wheel_building_kwargs(ireq):
    download_dir = os.path.join(CACHE_DIR, "pkgs")
    mkdir_p(download_dir)

    wheel_download_dir = os.path.join(CACHE_DIR, "wheels")
    mkdir_p(wheel_download_dir)

    if ireq.source_dir is not None:
        src_dir = ireq.source_dir
    elif ireq.editable:
        src_dir = _get_src_dir()
    else:
        src_dir = create_tracked_tempdir(prefix="reqlib-src")

    # This logic matches pip's behavior, although I don't fully understand the
    # intention. I guess the idea is to build editables in-place, otherwise out
    # of the source tree?
    if ireq.editable:
        build_dir = src_dir
    else:
        build_dir = create_tracked_tempdir(prefix="reqlib-build")

    return {
        "build_dir": build_dir,
        "src_dir": src_dir,
        "download_dir": download_dir,
        "wheel_download_dir": wheel_download_dir,
    }


def iter_egginfos(path, pkg_name=None):
    for entry in scandir(path):
        if entry.is_dir():
            if not entry.name.endswith("egg-info"):
                for dir_entry in iter_egginfos(entry.path, pkg_name=pkg_name):
                    yield dir_entry
            elif pkg_name is None or entry.name.startswith(pkg_name):
                yield entry


def find_egginfo(target, pkg_name=None):
    egg_dirs = (egg_dir for egg_dir in iter_egginfos(target, pkg_name=pkg_name))
    if pkg_name:
        yield next(iter(egg_dirs), None)
    else:
        for egg_dir in egg_dirs:
            yield egg_dir


def get_metadata(path, pkg_name=None):
    if pkg_name:
        pkg_name = packaging.utils.canonicalize_name(pkg_name)
    egg_dir = next(iter(find_egginfo(path, pkg_name=pkg_name)), None)
    if egg_dir is not None:
        import pkg_resources

        egg_dir = os.path.abspath(egg_dir.path)
        base_dir = os.path.dirname(egg_dir)
        path_metadata = pkg_resources.PathMetadata(base_dir, egg_dir)
        dist = next(
            iter(pkg_resources.distributions_from_metadata(path_metadata.egg_info)),
            None,
        )
        if dist:
            requires = dist.requires()
            dep_map = dist._build_dep_map()
            deps = []
            for k in dep_map.keys():
                if k is None:
                    deps.extend(dep_map.get(k))
                    continue
                else:
                    _deps = dep_map.get(k)
                    k = k.replace(":", "; ")
                    _deps = [
                        pkg_resources.Requirement.parse("{0}{1}".format(str(req), k))
                        for req in _deps
                    ]
                    deps.extend(_deps)
            return {
                "name": dist.project_name,
                "version": dist.version,
                "requires": requires,
            }


@attr.s(slots=True)
class SetupInfo(object):
    name = attr.ib(type=str, default=None)
    base_dir = attr.ib(type=Path, default=None)
    version = attr.ib(type=packaging.version.Version, default=None)
    extras = attr.ib(type=list, default=attr.Factory(list))
    requires = attr.ib(type=dict, default=attr.Factory(dict))
    build_requires = attr.ib(type=list, default=attr.Factory(list))
    build_backend = attr.ib(type=list, default=attr.Factory(list))
    setup_requires = attr.ib(type=dict, default=attr.Factory(list))
    python_requires = attr.ib(type=packaging.specifiers.SpecifierSet, default=None)
    extras = attr.ib(type=dict, default=attr.Factory(dict))
    setup_cfg = attr.ib(type=Path, default=None)
    setup_py = attr.ib(type=Path, default=None)
    pyproject = attr.ib(type=Path, default=None)
    ireq = attr.ib(default=None)
    extra_kwargs = attr.ib(default=attr.Factory(dict), type=dict)

    def parse_setup_cfg(self):
        if self.setup_cfg is not None and self.setup_cfg.exists():
            default_opts = {
                "metadata": {"name": "", "version": ""},
                "options": {
                    "install_requires": "",
                    "python_requires": "",
                    "build_requires": "",
                    "setup_requires": "",
                    "extras": "",
                },
            }
            parser = configparser.ConfigParser(default_opts)
            parser.read(self.setup_cfg.as_posix())
            if parser.has_option("metadata", "name"):
                name = parser.get("metadata", "name")
                if not self.name and name is not None:
                    self.name = name
            if parser.has_option("metadata", "version"):
                version = parser.get("metadata", "version")
                if not self.version and version is not None:
                    self.version = version
            if parser.has_option("options", "install_requires"):
                self.requires.update(
                    {
                        dep.strip(): init_requirement(dep.strip())
                        for dep in parser.get("options", "install_requires").split("\n")
                        if dep
                    }
                )
            if parser.has_option("options", "python_requires"):
                python_requires = parser.get("options", "python_requires")
                if python_requires and not self.python_requires:
                    self.python_requires = python_requires
            if parser.has_option("options", "extras_require"):
                self.extras.update(
                    {
                        section: [
                            dep.strip()
                            for dep in parser.get(
                                "options.extras_require", section
                            ).split("\n")
                            if dep
                        ]
                        for section in parser.options("options.extras_require")
                    }
                )

    def run_setup(self):
        if self.setup_py is not None and self.setup_py.exists():
            target_cwd = self.setup_py.parent.as_posix()
            with cd(target_cwd), _suppress_distutils_logs():
                script_name = self.setup_py.as_posix()
                args = ["egg_info", "--egg-base", self.base_dir]
                g = {"__file__": script_name, "__name__": "__main__"}
                local_dict = {}
                if sys.version_info < (3, 5):
                    save_argv = sys.argv
                else:
                    save_argv = sys.argv.copy()
                # This is for you, Hynek
                # see https://github.com/hynek/environ_config/blob/69b1c8a/setup.py
                try:
                    global _setup_distribution, _setup_stop_after
                    _setup_stop_after = "run"
                    sys.argv[0] = script_name
                    sys.argv[1:] = args
                    with open(script_name, 'rb') as f:
                        if sys.version_info < (3, 5):
                            exec(f.read(), g, local_dict)
                        else:
                            exec(f.read(), g)
                # We couldn't import everything needed to run setup
                except NameError:
                    python = os.environ.get('PIP_PYTHON_PATH', sys.executable)
                    out, _ = run([python, "setup.py"] + args, cwd=target_cwd, block=True,
                                 combine_stderr=False, return_object=False, nospin=True)
                finally:
                    _setup_stop_after = None
                    sys.argv = save_argv
                dist = _setup_distribution
                if not dist:
                    self.get_egg_metadata()
                    return

                name = dist.get_name()
                if name:
                    self.name = name
                if dist.python_requires and not self.python_requires:
                    self.python_requires = packaging.specifiers.SpecifierSet(
                        dist.python_requires
                    )
                if dist.extras_require and not self.extras:
                    self.extras = dist.extras_require
                install_requires = dist.get_requires()
                if not install_requires:
                    install_requires = dist.install_requires
                if install_requires and not self.requires:
                    requirements = [init_requirement(req) for req in install_requires]
                    self.requires.update({req.key: req for req in requirements})
                if dist.setup_requires and not self.setup_requires:
                    self.setup_requires = dist.setup_requires
                if not self.version:
                    self.version = dist.get_version()

    def get_egg_metadata(self):
        if self.setup_py is not None and self.setup_py.exists():
            metadata = get_metadata(self.setup_py.parent.as_posix(), pkg_name=self.name)
            if metadata:
                if not self.name:
                    self.name = metadata.get("name", self.name)
                if not self.version:
                    self.version = metadata.get("version", self.version)
                self.requires.update(
                    {req.key: req for req in metadata.get("requires", {})}
                )

    def run_pyproject(self):
        if self.pyproject and self.pyproject.exists():
            result = get_pyproject(self.pyproject.parent)
            if result is not None:
                requires, backend = result
                if backend:
                    self.build_backend = backend
                if requires and not self.build_requires:
                    self.build_requires = requires

    def get_info(self):
        if self.setup_cfg and self.setup_cfg.exists():
            self.parse_setup_cfg()
        if self.setup_py and self.setup_py.exists():
            if not self.requires or not self.name:
                try:
                    self.run_setup()
                except Exception:
                    self.get_egg_metadata()
                if not self.requires or not self.name:
                    self.get_egg_metadata()

        if self.pyproject and self.pyproject.exists():
            self.run_pyproject()
        return self.as_dict()

    def as_dict(self):
        prop_dict = {
            "name": self.name,
            "version": self.version,
            "base_dir": self.base_dir,
            "ireq": self.ireq,
            "build_backend": self.build_backend,
            "build_requires": self.build_requires,
            "requires": self.requires,
            "setup_requires": self.setup_requires,
            "python_requires": self.python_requires,
            "extras": self.extras,
            "extra_kwargs": self.extra_kwargs,
            "setup_cfg": self.setup_cfg,
            "setup_py": self.setup_py,
            "pyproject": self.pyproject,
        }
        return {k: v for k, v in prop_dict.items() if v}

    @classmethod
    def from_requirement(cls, requirement, finder=None):
        ireq = requirement.as_ireq()
        subdir = getattr(requirement.req, "subdirectory", None)
        return cls.from_ireq(ireq, subdir=subdir, finder=finder)

    @classmethod
    def from_ireq(cls, ireq, subdir=None, finder=None):
        import pip_shims.shims

        if ireq.link.is_wheel:
            return
        if not finder:
            from .dependencies import get_finder

            finder = get_finder()
        kwargs = _prepare_wheel_building_kwargs(ireq)
        ireq.populate_link(finder, False, False)
        ireq.ensure_has_source_dir(kwargs["build_dir"])
        if not (
            ireq.editable
            and pip_shims.shims.is_file_url(ireq.link)
            and not ireq.link.is_artifact
        ):
            if ireq.is_wheel:
                only_download = True
                download_dir = kwargs["wheel_download_dir"]
            else:
                only_download = False
                download_dir = kwargs["download_dir"]
        ireq_src_dir = None
        if ireq.link.scheme == "file":
            path = pip_shims.shims.url_to_path(unquote(ireq.link.url_without_fragment))
            if pip_shims.shims.is_installable_dir(path):
                ireq_src_dir = path
        if not ireq.editable or not (pip_shims.is_file_url(ireq.link) and ireq_src_dir):
            pip_shims.shims.unpack_url(
                ireq.link,
                ireq.source_dir,
                download_dir,
                only_download=only_download,
                session=finder.session,
                hashes=ireq.hashes(False),
                progress_bar="off",
            )
        if ireq.editable:
            created = cls.create(
                ireq.source_dir, subdirectory=subdir, ireq=ireq, kwargs=kwargs
            )
        else:
            build_dir = ireq.build_location(kwargs["build_dir"])
            ireq._temp_build_dir.path = kwargs["build_dir"]
            created = cls.create(
                build_dir, subdirectory=subdir, ireq=ireq, kwargs=kwargs
            )
        created.get_info()
        return created

    @classmethod
    def create(cls, base_dir, subdirectory=None, ireq=None, kwargs=None):
        if not base_dir or base_dir is None:
            return

        creation_kwargs = {"extra_kwargs": kwargs}
        if not isinstance(base_dir, Path):
            base_dir = Path(base_dir)
        creation_kwargs["base_dir"] = base_dir.as_posix()
        pyproject = base_dir.joinpath("pyproject.toml")

        if subdirectory is not None:
            base_dir = base_dir.joinpath(subdirectory)
        setup_py = base_dir.joinpath("setup.py")
        setup_cfg = base_dir.joinpath("setup.cfg")
        creation_kwargs["pyproject"] = pyproject
        creation_kwargs["setup_py"] = setup_py
        creation_kwargs["setup_cfg"] = setup_cfg
        if ireq:
            creation_kwargs["ireq"] = ireq
        return cls(**creation_kwargs)
