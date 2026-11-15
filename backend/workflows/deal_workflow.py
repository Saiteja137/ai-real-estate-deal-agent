from backend.agents.property_agent import extract
from backend.agents.email_agent import draft
from backend.tools.deal_evaluation import evaluate
from backend.agents.evaluation_agent import assess_property
from backend.tools.roi_calculator import metrics
from backend import config

def run_workflow(subject, body, sender, criteria):
    from typing import TypedDict, Any
    from langgraph.graph import StateGraph, START, END
    class State(TypedDict, total=False):
        subject: str
        body: str
        sender: str
        criteria: dict
        properties: list
        source: str
        results: list
    def extraction_node(s):
        properties, source = extract(s['subject'], s['body'])
        return {'properties': properties, 'source': source}
    def evaluation_node(s):
        results=[]
        for p in s['properties']:
            if config.AI_ENABLED:
                missing=[name for name in ('address','property_type','asking_price','arv','repairs','closing_costs','assignment_fee','selling_costs') if p.get(name) is None]
                context={'metrics':metrics(p,s['criteria']),'criteria':s['criteria'],'missing':missing}
                assessment=assess_property(p,context)
                ev={**context,**assessment,'evaluation_source':'openai',
                    'assumptions':['Unknown costs are not treated as zero; verify wholesaler-provided figures.']}
            else:
                ev=evaluate(p,s['criteria'])
                ev['evaluation_source']='python_rules'
            ev['initial_draft_source']='openai' if config.AI_ENABLED else 'template'
            results.append({'property':p,'evaluation':ev,'draft':draft(p,ev,s['sender'],s['subject'])})
        return {'results':results}
    graph=StateGraph(State)
    graph.add_node('extract',extraction_node); graph.add_node('evaluate_and_draft',evaluation_node)
    graph.add_edge(START,'extract'); graph.add_edge('extract','evaluate_and_draft'); graph.add_edge('evaluate_and_draft',END)
    return graph.compile().invoke({'subject':subject,'body':body,'sender':sender,'criteria':criteria})


