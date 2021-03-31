from setuptools import setup

setup(
    name="qnt",
    version="0.0.234",
    url="https://quantnet.ai",
    license='MIT',
    packages=['qnt', 'qnt.ta', 'qnt.data', 'qnt.examples'],
    package_data={'qnt': ['*.ipynb']},
    install_requires=['xarray', 'pandas', 'numpy', 'scipy', 'tabulate', 'bottleneck', 'numba', 'progressbar2']
)