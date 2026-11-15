"""Validated backend tool registry. Sending is never callable by a model."""
import json
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from backend.agents.property_agent import extract
from backend.agents.email_agent import draft
from backend.tools.roi_calculator import metrics
from backend.tools.deal_evaluation import evaluate
from backend.database import db, row, audit
from backend.services.approval_service import create_approval

class Args(BaseModel): model_config=ConfigDict(extra='forbid')
class Empty(Args): pass
class ExtractionArgs(Args):
    subject:str=Field(max_length=500)
    body:str=Field(max_length=50000)
class DealArgs(Args):
    property:dict[str,Any]
    criteria:dict[str,Any]
class ReplyArgs(DealArgs):
    sender:str=Field(max_length=320)
    subject:str=Field(max_length=500)
class IDArgs(Args): id:str=Field(min_length=1,max_length=128)

def criteria():
    with db() as c: return json.loads(c.execute("SELECT value FROM settings WHERE key='criteria'").fetchone()[0])
def status(id):
    with db() as c:
        a=row(c,'approvals',id)
        return {'id':a['id'],'status':a['status'],'deadline':a['deadline']}
def missing(property,criteria): return evaluate(property,criteria)['missing']
def execute_approved_reply(**kwargs): raise PermissionError('Execution is restricted to approval processing')
REGISTRY={
 'extract_property_details':(ExtractionArgs,lambda subject,body:extract(subject,body)),
 'get_buying_criteria':(Empty,criteria),
 'calculate_deal_metrics':(DealArgs,lambda property,criteria:metrics(property,criteria)),
 'evaluate_property_deal':(DealArgs,lambda property,criteria:evaluate(property,criteria)),
 'get_missing_information':(DealArgs,missing),
 'generate_wholesaler_reply':(ReplyArgs,lambda property,criteria,sender,subject:draft(property,evaluate(property,criteria),sender,subject)),
 'create_email_approval':(IDArgs,lambda id:create_approval(id)),
 'get_approval_status':(IDArgs,status),
 'execute_approved_reply':(IDArgs,execute_approved_reply),
}
def invoke(name,args,*,trusted=False):
    if name not in REGISTRY: raise ValueError('Unknown tool')
    if name in {'create_email_approval','execute_approved_reply'} and not trusted: raise PermissionError('Trusted backend caller required')
    schema,fn=REGISTRY[name]; validated=schema.model_validate(args)
    try: result=fn(**validated.model_dump())
    except Exception as exc:
        with db() as c: audit(c,'tool_error',{'tool':name,'error_type':type(exc).__name__})
        raise
    with db() as c: audit(c,'tool_execution',{'tool':name,'result':'success'})
    return result
