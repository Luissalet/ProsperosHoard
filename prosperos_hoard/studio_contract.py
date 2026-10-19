"""Static common project/control tools; importing this never imports an engine."""


def entry(name, description, properties=None, required=(), *, read=False, draft=True):
    return {'name': name, 'description': description + '\nKeywords: audiovisual, project, timeline, proyecto, montaje',
        'inputSchema': {'type': 'object', 'properties': properties or {}, 'required': list(required)},
        'annotations': {'readOnlyHint': read, 'destructiveHint': False, 'draftSafeHint': not read and draft, 'openWorldHint': False}}


TOOLS = [
    entry('audiovisual_objects', 'List native projects, scenes and timelines in enabled modules without enabling others / ver escenas y cortes nativos.', read=True),
    entry('studio_modules', 'List module state without starting engines / ver módulos sin arrancar motores.', read=True),
    entry('studio_module_set', 'Explicitly enable/stop one audiovisual module / activar o apagar un módulo.',
          {'module': {'type':'string','enum':['generation','editing']}, 'enabled':{'type':'boolean'},
           'automatic':{'type':'boolean','default':False}}, ['module','enabled'], draft=False),
    entry('audiovisual_projects', 'Read persistent audiovisual projects and revisions while modules are OFF / leer proyectos y revisiones.',
          {'project':{'type':'string'}}, read=True),
    entry('studio_project_create', 'Create an audiovisual project draft / crear proyecto audiovisual.',
          {'title':{'type':'string','minLength':1,'maxLength':200}}, ['title']),
    entry('studio_project_transfer', 'Bring a generated cut into the complete native editor, retaining original scenes, files and revisions / llevar corte al montaje.',
          {'project':{'type':'string'},'timeline_id':{'type':'string'},'production':{'type':'string'}}, ['project','timeline_id']),
]
NAMES = {item['name'] for item in TOOLS}
