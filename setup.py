"""
Flask-WhooshAlchemy
-------------

Whoosh extension to Flask/SQLAlchemy
"""

import os
from setuptools import setup

SRC_PATH = os.path.dirname(__file__)


def get_version():
    with open(os.path.join(SRC_PATH, 'flask_whooshalchemyplus.py')) as f:
        for l in f.readlines():
            if l.startswith('__version__'):
                exec (l)
                return locals().get('__version__')


def get_requirements():
    with open(os.path.join(SRC_PATH, 'requirements.txt')) as f:
        return [x.strip() for x in f.readlines()]


def get_readme():
    with open(os.path.join(SRC_PATH, 'README.rst')) as f:
        return f.read()


VERSION = get_version()
REQUIRES = get_requirements()
README = get_readme()

setup(
    name='Flask-WhooshAlchemyPlus',
    version=VERSION,
    url='https://github.com/revolution1/Flask-WhooshAlchemyPlus',
    license='BSD',
    author='Revolution1',
    author_email='crj93106@gmail.com',
    maintainer='Revolution1',
    maintainer_email='crj93106@gmail.com',
    description='Whoosh extension to Flask/SQLAlchemy',
    long_description=README,
    py_modules=['flask_whooshalchemyplus'],
    provides=['flask_whooshalchemyplus'],
    zip_safe=False,
    include_package_data=True,
    platforms='any',
    install_requires=REQUIRES,
    requires=REQUIRES,
    tests_require=['Flask-Testing'],

    classifiers=[
        'Environment :: Web Environment',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Topic :: Internet :: WWW/HTTP :: Dynamic Content',
        'Topic :: Software Development :: Libraries :: Python Modules'
    ],
    test_suite='test.test_all',
)
