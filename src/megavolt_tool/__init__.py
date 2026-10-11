"""The internal pricing tool: sales or support picks a customer and reads the offer.

Runs on the laptop, bound to loopback, server-rendered. It reads the warehouse as a role that can
only SELECT the models dbt builds, and calls the private pricing engine, which is installed only
where this tool runs. Nothing about how a price is formed lives in this package.
"""
