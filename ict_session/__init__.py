"""ICT Asian range -> London sweep -> NY MSS/FVG session-liquidity dashboard module.

Register in your Flask app:
    from ict_session import ict_bp, start_ict_scheduler
    app.register_blueprint(ict_bp)          # dashboard at /ict/
    start_ict_scheduler(app)                # optional background scans (credit-aware)
"""
from .routes import ict_bp, start_ict_scheduler  # noqa: F401

__version__ = "1.1.0"
