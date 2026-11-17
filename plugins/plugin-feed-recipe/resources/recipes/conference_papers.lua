return {
  id = "conference_papers",
  name = "顶会与前沿论文 (cs.AI)",
  category = "journal",
  format = "xml",
  use_chrome_cookies = false,
  tags = { "AI", "Conference", "Papers" },
  feed = {
    url = "https://rss.arxiv.org/rss/cs.AI",
    max_items = 25,
  },
  article = {
    url = "{{url}}",
    mode = "readability",
    body_selector = "blockquote.abstract, .abstract, main",
  },
  transform_item = function(item)
    if item.title then item.title = item.title:gsub("^%s+", ""):gsub("%s+$", "") end
    return item
  end,
}
