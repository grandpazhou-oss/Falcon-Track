"""Fixed-source, public-page collectors for Logistics Lead Radar.

Run the Scrapy process via ``python -m collectors.cli``.  The process boundary
keeps Twisted's reactor lifecycle out of Django/Prefect application processes.
"""

__version__ = "0.1.0"
