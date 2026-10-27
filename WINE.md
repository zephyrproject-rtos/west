https://www.winehq.org/ provides a way to run Windows binaries on Linux.

Q: Why would a west developer need WINE considering west is portable Python code?
A: To quickly put themselves in the shoes of Windows users and troubleshoot
   Windows-specific issues without:

- A Windows licence
- A different computer to run it, or the inconvenience of dual-booting
- The inconvenience of synchronizing source files across computers

Warning: WINE is not 100% identical to Windows and WINE is _not_ able to replicate all
issues happening on Windows. But enough issues can be reproduced, including Path-related
issues.

This is a quick and dirty recipe to run west tests using WINE.  This process is not
officially supported and these instructions can "bitrot" and may have fallen apart by the
time you read them: you have been warned. Please submit fixes or better ways if you know
them. If humanly possible, keep this working over ssh. That is: without any GUI.

Do NOT replace Windows by WINE to validate releases!

WINE
----

First you need to install WINE. Try the following commands and your Linux distribution will
likely tell you what packages are missing:

```
wine   --help
wine64 --help
wine32 --help
```
At least one of these must print WINE's help message before you proceed.

Some distributions let you install both at the same time. To find which one the "wine"
command currently points at:
```
wine cmd
dir c:
```
If you see _both_ `Program Files` _and_ `Program Files (x86)`, then you are running 64 bits WINE.

WENV
----

wenv seems to work and to simplify things greatly.
https://wenv.readthedocs.io/en/latest/usage.html

```
pip install wenv
export WENV_PYTHONVERSION=3.12.10 # or any other version that works
export WENV_ARCH=win64            # Optional - wenv defaults to WINE 32 bits.
wenv init
# wenv help should now list "python" and "pip"
wenv help
# Among others, Python's interactive prompt shows whether it's 32 or 64 bits
wenv python
# Warning: pykwalify is being replaced by jsonschema, see #904 and west/pyproject.toml
wenv pip -v install setuptools
wenv pip -v install pyyaml pykwalify pytest pytest-xdist pytest-env
```

An alternative to the WENV_* environment variables is:
```
echo '{ "pythonversion": "3.12.10" }' > ~/.wenv.json
# For WINE64
echo '{ "pythonversion": "3.12.10", "arch":"win64" }' > ~/.wenv.json
```

Download MinGit-*-.zip from https://github.com/git-for-windows/git/releases/
and install it in the wenv C: disk:
```
unzip -d ~/.local/share/wenv/win*/drive_c/MinGit/  MinGit-*.zip
```

Append this at the end of `west/pyproject.toml`

```
# TODO: find a way to do this in "wenv" instead
# Useful tip: pytest --pytest-env-verbose ...
[tool.pytest_env]
PATH = { value = "{PATH};C:\\MinGit\\cmd", transform = true }
```

At last:
```
cd west
wenv pytest -h
wenv pytest -v -s -k round_trip -n auto
```

If unused, delete the current $WENV_ARCH + $WENV_PYTHONVERSION combination and reclaim a couple Gigabytes:
```
wenv help
wenv clean
wenv help
```


