from setuptools import setup, find_packages
from takershield import __version__

setup(
    name="takershield",
    version=__version__,
    description="Real-time risk monitoring for Kalshi market makers",
    author="TakerShield",
    url="https://github.com/takershield/takershield-observer",
    packages=find_packages(),
    install_requires=[
        "websockets>=11.0.0",
        "rich>=13.0.0",
    ],
    entry_points={
        "console_scripts": [
            "takershield=takershield.observer:main",
        ],
    },
    python_requires=">=3.8",
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
)
