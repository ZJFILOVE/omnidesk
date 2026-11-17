// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { ArticleImage } from '../ArticleImage';
import { PluginResultView } from '../../workbenches/PluginResultView';
import { TextArticleReader } from '../../workbenches/panels/TextArticleReader';
import type { TextArticle } from '../../data/mock';

afterEach(cleanup);

describe('article images and answer boundaries', () => {
  it('renders answer headings and a separate captioned figure in plugin results', () => {
    const { container } = render(<PluginResultView actionId="fetch_article" payload={{ article: {
      title: '问题', paragraphs: [
        { id: 'ans-1-hdr', kind: 'heading', original: '回答 1 · 甲' },
        { id: 'a1-1', kind: 'image', image_url: 'https://pic1.zhimg.com/full.jpg', alt: '鸭鸭', caption: '品牌图片' },
        { id: 'a1-2', kind: 'paragraph', original: '第一条回答正文' },
        { id: 'ans-2-hdr', kind: 'heading', original: '回答 2 · 乙' },
        { id: 'a2-1', kind: 'paragraph', original: '第二条回答正文' },
      ],
    } }} />);
    expect(screen.getByRole('heading', { level: 3, name: '回答 2 · 乙' })).not.toBeNull();
    expect(screen.getByRole('img', { name: '鸭鸭' }).getAttribute('referrerpolicy')).toBe('no-referrer');
    expect(container.querySelector('figure figcaption')?.textContent).toBe('品牌图片');
    expect(container.querySelectorAll('figure')).toHaveLength(1);
  });

  it('keeps images visible in translated-only reading mode', () => {
    const article = { id: 'test', tags: [], blocks: [{ id: 'img', kind: 'image', original: '', imageUrl: 'https://i.redd.it/test.png', alt: 'Plot' }] } as unknown as TextArticle;
    const noop = () => {};
    render(<TextArticleReader article={article} bilingualMode="zh" activeFocusId="" onFocus={noop}
      onBilingualModeChange={noop} onSendToBabelDoc={noop} onOpenChromeApp={noop} onSelectBlock={noop} />);
    expect(screen.getAllByRole('img', { name: 'Plot' })).toHaveLength(1);
  });

  it('rejects unsafe images and offers the original when loading fails', () => {
    const { rerender } = render(<ArticleImage url="javascript:alert(1)" alt="unsafe" />);
    expect(screen.queryByRole('img')).toBeNull();
    rerender(<ArticleImage url="https://i.redd.it/test.png" alt="Plot" />);
    fireEvent.error(screen.getByRole('img'));
    expect(screen.getByRole('link', { name: '打开原图' }).getAttribute('href')).toBe('https://i.redd.it/test.png');
  });
});
