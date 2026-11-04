import asyncio
from rich.text import Text
from rich.markdown import Markdown
from textual.app import App
from textual.widgets import Static
from textual.geometry import Region
from panda_cli.cockpit import ToolCard

PAY = "A\x1b]52;c;U1lOVEg=\x07B\x1b[2JC\x9b31mD"
class T(App):
    def compose(self):
        yield ToolCard("run_shell", PAY)
        yield Static(Markdown("hello " + PAY), id="md")
async def main():
    app = T()
    async with app.run_test(size=(100, 10)) as pilot:
        await pilot.pause()
        for w in app.query(Static):
            strips = w.render_lines(Region(0, 0, w.size.width, w.size.height))
            text = "".join(seg.text for s in strips for seg in s)
            print(type(w).__name__, repr(text.strip()[:80]), "ESC" in text or "\x1b" in text, "\x9b" in text)
asyncio.run(main())
