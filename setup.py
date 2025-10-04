from setuptools import setup, find_packages


setup(
    name='petritype',
    version='0.1.0',
    packages=find_packages(),
    install_requires=[
        "pydantic==2.11.9",
        "rustworkx==0.14.2",
        "setuptools==80.9.0",
    ],
    entry_points={
        'console_scripts': [
            # If you have scripts to expose, list them here
        ],
    },
)
