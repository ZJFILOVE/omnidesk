// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { PluginResultView } from '../../workbenches/PluginResultView';
import { toBlocks } from '../../hooks/useLiveDomain';
vi.mock('../../api', () => ({ isTauriAvailable: () => false }));
afterEach(cleanup);
it('renders both image blocks and legacy X attachments, with original-image fallback', () => {
  render(<PluginResultView actionId="fetch_article" payload={{ article: { id: 'tweet', title: '图片帖子', paragraphs: [
    { id: 'photo1', kind: 'image', image_url: 'https://pbs.twimg.com/media/a.jpg', alt: '第一张图' },
    { id: 'photo2', kind: 'paragraph', original: '[图片附件] https://pbs.twimg.com/media/b.jpg' },
  ], comments: [{ id: 'c', body: '讨论内容' }] } }} />);
  expect(screen.getAllByRole('img')).toHaveLength(2);
  expect(screen.getByText('讨论内容')).toBeTruthy();
  fireEvent.error(screen.getByAltText('第一张图'));
  expect(screen.getByText('打开原图')).toBeTruthy();
});
it('keeps structured and legacy image fields when mapping live article data', () => {
  const blocks = toBlocks([{ original: '[图片附件] https://example.org/p.jpg (图注)' }, { kind: 'image', image_url: 'https://example.org/q.jpg' }]);
  expect(blocks[0]).toMatchObject({ kind: 'image', imageUrl: 'https://example.org/p.jpg', alt: '图注' });
  expect(blocks[1].imageUrl).toBe('https://example.org/q.jpg');
});
