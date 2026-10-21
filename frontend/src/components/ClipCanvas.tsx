import { useEffect, useState, type ReactNode } from "react";
import { ReactFlow, Background, Controls, Handle, Position, useNodesState, type NodeProps, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

type ClipNode = Node<{ content: ReactNode; source?: boolean; input?: boolean }, "clipCard">;
function ClipCard({ data }: NodeProps<ClipNode>) {
  return <div className="clip-node">
    {(!data.source || data.input) && <Handle type="target" position={Position.Left} isConnectable={false} />}
    <div className="nowheel">{data.content}</div>
    {data.source && <Handle type="source" position={Position.Right} isConnectable={false} />}
  </div>;
}
const nodeTypes = { clipCard: ClipCard };

/** A small, real reference-to-render canvas. References are connected when
 * chosen, so there is no wiring ceremony before the first generation. */
export function ClipCanvas({ reference, references, guides, video, label }: {
  reference: ReactNode; references?: ReactNode; guides: { id: string; content: ReactNode }[]; video: ReactNode; label: string;
}) {
  const [compact, setCompact] = useState(() => window.matchMedia("(max-width: 800px)").matches);
  const [nodes, setNodes, onNodesChange] = useNodesState<ClipNode>([]);
  useEffect(() => {
    const query = window.matchMedia("(max-width: 800px)");
    const update = () => setCompact(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const wanted: ClipNode[] = [
      ...(references ? [{ id: "references", type: "clipCard" as const, position: { x: 0, y: 20 }, style: { width: 260 }, data: { source: true, content: references } }] : []),
      { id: "reference", type: "clipCard", position: { x: 0, y: references ? 340 : 20 }, style: { width: 260 }, data: { source: true, input: !!references, content: reference } },
      ...guides.map((guide, i): ClipNode => ({ id: guide.id, type: "clipCard", position: { x: 0, y: (references ? 880 : 560) + i * 540 }, style: { width: 260 }, data: { source: true, content: guide.content } })),
      { id: "video", type: "clipCard", position: { x: 370, y: 20 }, style: { width: 640 }, data: { content: video } },
    ];
    setNodes(previous => wanted.map(node => ({ ...node, dragHandle: ".clip-node-title, h2", position: previous.find(old => old.id === node.id)?.position || node.position })));
  }, [reference, references, guides, video, setNodes]);
  if (compact) return <div className="clip-canvas-mobile" aria-label={label}>
    <div className="clip-mobile-references">{references && <div className="clip-node">{references}</div>}{reference}{guides.map(guide => <div key={guide.id}>{guide.content}</div>)}</div>
    <div className="clip-node">{video}</div>
  </div>;
  return <div className="clip-canvas" aria-label={label}>
    <ReactFlow nodes={nodes} nodeTypes={nodeTypes} onNodesChange={onNodesChange}
      edges={[...["reference", ...guides.map(guide => guide.id)].map(source => ({ source, target: "video" })), ...(references ? [{ source: "references", target: "reference" }] : [])].map(edge => ({ ...edge, id: `${edge.source}-${edge.target}`, type: "default", style: { stroke: "var(--accent)", strokeWidth: 2 } }))}
      nodesConnectable={false} edgesFocusable={false} deleteKeyCode={null} minZoom={0.5} maxZoom={1.5}
      defaultViewport={{ x: 34, y: 16, zoom: 0.9 }} panOnScroll zoomOnScroll={false} fitView={false}>
      <Background color="var(--border-strong)" gap={24} size={1} />
      <Controls showInteractive={false} />
    </ReactFlow>
  </div>;
}
