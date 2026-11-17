// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { MarkdownRenderer } from '../MarkdownRenderer';

afterEach(() => {
  cleanup();
});

describe('MarkdownRenderer', () => {
  it('renders headings, paragraphs, quotes, lists, GFM tables, and safe images', () => {
    const md = [
      '# Title Heading',
      '',
      'Regular paragraph with **bold** and `inline_code`.',
      '',
      '> Quoted insight from reviewer.',
      '',
      '- Bullet one',
      '- Bullet two',
      '',
      '1. Ordered one',
      '2. Ordered two',
      '',
      '| Method | Score |',
      '| --- | --- |',
      '| Entropy | 96.8% |',
      '',
      '![Architecture Diagram](https://example.com/diagram.png)',
    ].join('\n');

    const { container } = render(<MarkdownRenderer content={md} />);

    expect(screen.getByRole('heading', { level: 1, name: 'Title Heading' })).not.toBeNull();
    expect(container.querySelector('blockquote')?.textContent).toContain('Quoted insight');
    expect(container.querySelectorAll('ul li')).toHaveLength(2);
    expect(container.querySelectorAll('ol li')).toHaveLength(2);
    expect(container.querySelector('table')).not.toBeNull();
    expect(screen.getByRole('cell', { name: '96.8%' })).not.toBeNull();

    const img = screen.getByRole('img', { name: 'Architecture Diagram' }) as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('https://example.com/diagram.png');
  });

  it('renders numbered heading blocks as headings instead of ordered lists', () => {
    const { container } = render(
      <MarkdownRenderer content="1. Problem & Empirical Observation" blockKind="heading" />,
    );
    expect(container.querySelector('ol')).toBeNull();
    const heading = container.querySelector('h3');
    expect(heading).not.toBeNull();
    expect(heading?.textContent).toBe('1. Problem & Empirical Observation');
  });

  it('renders inline and display KaTeX formulas including raw formula blocks', () => {
    const { container, rerender } = render(
      <MarkdownRenderer content={'Entropy $H(\\alpha_t)$ bounds drift.'} />,
    );
    expect(container.querySelector('.katex')).not.toBeNull();
    expect(container.querySelector('.katex-mathml')).not.toBeNull();

    rerender(
      <MarkdownRenderer
        content={'L_{\\text{evict}}(\\theta) = \\mathbb{E}[\\log q_\\theta(x)]'}
        blockKind="formula"
      />,
    );
    expect(container.querySelector('.katex-display')).not.toBeNull();
    expect(container.querySelector('.katex')).not.toBeNull();
  });

  it('renders syntax-highlighted code blocks with language badge and horizontal scroll', () => {
    const { container } = render(
      <MarkdownRenderer
        content={'def greet(name: str) -> str:\n    return f"hello {name}"'}
        blockKind="code"
        language="python"
      />,
    );

    const badge = screen.getByTestId('markdown-code-lang');
    expect(badge.textContent).toBe('python');
    expect(badge.getAttribute('data-code-lang')).toBe('python');

    const pre = container.querySelector('pre');
    expect(pre).not.toBeNull();
    expect(pre?.className).toContain('overflow-x-auto');
    expect(container.querySelector('.hljs-keyword')).not.toBeNull();
  });

  it('blocks dangerous javascript:/vbscript:/data: links and images while hardening external links', () => {
    const md = [
      '[Safe External](https://nature.com/articles/123)',
      '',
      '[Relative Doc](/docs/spec)',
      '',
      '[XSS Link](javascript:alert(1))',
      '',
      '[Obfuscated XSS](JaVaScRiPt:alert(2))',
      '',
      '[Data Link](data:text/html,<script>alert(1)</script>)',
      '',
      '![Bad SVG](data:image/svg+xml;base64,PHN2Zz4=)',
    ].join('\n');

    const { container } = render(<MarkdownRenderer content={md} />);

    const externalLink = screen.getByRole('link', { name: 'Safe External' });
    expect(externalLink.getAttribute('href')).toBe('https://nature.com/articles/123');
    expect(externalLink.getAttribute('rel')).toBe('noopener noreferrer');
    expect(externalLink.getAttribute('target')).toBe('_blank');

    const relLink = screen.getByRole('link', { name: 'Relative Doc' });
    expect(relLink.getAttribute('href')).toBe('/docs/spec');
    expect(relLink.getAttribute('target')).toBeNull();

    const allLinks = Array.from(container.querySelectorAll('a'));
    expect(allLinks).toHaveLength(2);
    for (const a of allLinks) {
      expect(a.getAttribute('href')?.toLowerCase()).not.toContain('javascript:');
      expect(a.getAttribute('href')?.toLowerCase()).not.toContain('data:');
    }

    expect(container.querySelectorAll('[data-blocked-link="true"]')).toHaveLength(3);
    expect(screen.getByText('XSS Link')).not.toBeNull();
    expect(container.querySelectorAll('[data-blocked-image="true"]')).toHaveLength(1);
    expect(container.querySelector('img')).toBeNull();
  });
});
