# Bundled wheels

`proxy_tools-0.1.0-py3-none-any.whl`: a dependency of pywebview (the app window). PyPI only has its
source package (`proxy_tools-0.1.0.tar.gz`, MIT, sha256 `ccb3751f529c047e…`), so pip would build it on
your PC during the first install. This wheel was built from that source package with no changes
(`proxy_tools/__init__.py` is byte-identical) and lets `start.bat` install everything pre-built.
