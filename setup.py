#!/usr/bin/python

from setuptools import setup, find_packages


setup(
    name='zeit.talk',
    url='https://github.com/rdrongow/zeit.web',
    version='0.1dev',
    author=(
        'Ron Drongowski'
    ),
    author_email=(
        'ron.drongowski@zeit.de'
    ),
    install_requires=[
        'zeit.web>=3.108.1',
    ],
    description='This package is all about ZEIT ONLINE website delivery.',
    long_description=open('README.md', 'r').read(),
    entry_points={
        'paste.app_factory': [
            'main=zeit.talk.talk:factory'
        ]
    },
    extras_require={
        'test': [
            'cssselect',
            'gocept.httpserverlayer',
            'mock',
            'plone.testing [zca,zodb]',
            'pytest',
            'pytest-pep8',
            'pytest-timeout',
            'selenium',
            'transaction',
            'waitress',
            'webtest',
            'wesgi',
            'zope.event',
            'zope.testbrowser [wsgi]'
        ]
    },
    setup_requires=['setuptools_git'],
    namespace_packages=['zeit'],
    packages=find_packages('src'),
    package_dir={'': 'src'},
    include_package_data=True,
    zip_safe=False,
    keywords='web wsgi pyramid zope',
    license='Proprietary license',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Framework :: Pyramid',
        'Framework :: Zope3',
        'Intended Audience :: Developers',
        'License :: Other/Proprietary License',
        'Operating System :: Unix',
        'Programming Language :: Python :: 2.7',
        'Topic :: Internet :: WWW/HTTP :: WSGI'
    ]
)
