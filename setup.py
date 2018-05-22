from setuptools import setup, find_packages
import aioacm

try:
    import pypandoc
    long_description = pypandoc.convert('README.md', 'rst')
except(IOError, ImportError):
    long_description = open('README.md').read()

setup(
    name="aioacm-sdk-python",
    version=aioacm.__version__,
    packages=find_packages(exclude=["test"]),
    url="https://github.com/imopio/aioacm-sdk-python",
    license="Apache License 2.0",
    author="acm",
    author_email="755063194@qq.com",
    description="Python client for ACM with asyncio support.",
    long_description=long_description,
    install_requires=[
        "aiohttp",
        "aliyunsdkcore",
        "aliyun-python-sdk-kms"
    ],
)
