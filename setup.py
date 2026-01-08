from setuptools import setup, find_packages

setup(
    name="takershield",
    version="0.1.0",
    description="Real-time risk monitoring client for Kalshi markets",
    author="TakerShield AI",
    url="https://github.com/takershield/observer",
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
