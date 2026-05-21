# Packaging and Distribution

## pip (primary path)

The package name is `telos-sdk`, already configured in `pyproject.toml`.

```bash
# local development install
pip install -e .

# build sdist + wheel
python -m build

# publish to PyPI
twine upload dist/*
```

After publishing, users can simply:

```bash
pip install telos-sdk
telos --help
```

## Homebrew (template)

`telos-sdk.rb` is a Homebrew formula template. Only the template is provided here; publishing the tap is left for later.

Full steps:

1. Publish to PyPI first (see above).
2. Compute the sha256 of the sdist:
   ```bash
   shasum -a 256 dist/telos_sdk-0.1.0.tar.gz
   ```
3. Fill `url` / `sha256` into `telos-sdk.rb`.
4. Auto-generate the dependency resource blocks:
   ```bash
   brew update-python-resources telos-sdk.rb
   ```
5. Create the tap repository `telos-pro/homebrew-telos` and place `telos-sdk.rb` under `Formula/`.
6. Validate:
   ```bash
   brew audit --strict --new packaging/telos-sdk.rb
   ```

After publishing, users can simply:

```bash
brew install telos-pro/telos/telos-sdk
```
