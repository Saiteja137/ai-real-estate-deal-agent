from backend.agents.property_agent import extract
from backend.agents.email_agent import draft
from backend.tools.deal_evaluation import evaluate

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
            ev=evaluate(p,s['criteria'])
            results.append({'property':p,'evaluation':ev,'draft':draft(p,ev,s['sender'],s['subject'])})
        return {'results':results}
    graph=StateGraph(State)
    graph.add_node('extract',extraction_node); graph.add_node('evaluate_and_draft',evaluation_node)
    graph.add_edge(START,'extract'); graph.add_edge('extract','evaluate_and_draft'); graph.add_edge('evaluate_and_draft',END)
    return graph.compile().invoke({'subject':subject,'body':body,'sender':sender,'criteria':criteria})


