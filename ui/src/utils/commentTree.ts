import type { CommentNode } from '../data/mock';

export type BilingualMode = 'bilingual' | 'zh' | 'en';

export function countComments(nodes: CommentNode[]): number {
  return nodes.reduce(
    (sum, node) => sum + 1 + (node.children ? countComments(node.children) : 0),
    0,
  );
}

export function countDescendants(node: CommentNode): number {
  return node.children ? countComments(node.children) : 0;
}

export interface FlattenedCommentItem {
  id: string;
  node: CommentNode;
  depth: number;
  parentId: string | null;
  parentFocusId: string | null;
  hasChildren: boolean;
  descendantCount: number;
  firstChildFocusId: string | null;
  focusId: string;
  voteFocusId: string;
  toggleFocusId: string;
}

export function flattenCommentTree(
  nodes: CommentNode[],
  collapsedMap: Record<string, boolean>,
): FlattenedCommentItem[] {
  const out: FlattenedCommentItem[] = [];

  const visit = (list: CommentNode[], depth: number, parentId: string | null) => {
    for (const node of list) {
      const children = node.children ?? [];
      const hasChildren = children.length > 0;
      const descendantCount = countDescendants(node);
      const isCollapsed = Boolean(collapsedMap[node.id]);
      const focusId = `text:comment:node:${node.id}`;
      out.push({
        id: node.id,
        node,
        depth,
        parentId,
        parentFocusId: parentId ? `text:comment:node:${parentId}` : null,
        hasChildren,
        descendantCount,
        firstChildFocusId:
          hasChildren && !isCollapsed ? `text:comment:node:${children[0].id}` : null,
        focusId,
        voteFocusId: `text:comment:vote:${node.id}`,
        toggleFocusId: `text:comment:toggle:${node.id}`,
      });
      if (hasChildren && !isCollapsed) {
        visit(children, depth + 1, node.id);
      }
    }
  };

  visit(nodes, 0, null);
  return out;
}
