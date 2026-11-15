"""Compatibility exports; implementations live in focused backend modules."""
from backend.config import DEFAULT_CRITERIA
from backend.schemas.property import Property, Properties
from backend.tools.roi_calculator import metrics
from backend.tools.deal_evaluation import evaluate
from backend.agents.property_agent import extract
from backend.agents.email_agent import draft
from backend.workflows.deal_workflow import run_workflow
