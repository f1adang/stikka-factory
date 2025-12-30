"""FAQ tab content."""

import streamlit as st
from PIL import Image


def render():
    """Render the FAQ tab."""
    st.subheader("Info")
    st.markdown(
        """
        - Web: [stikka.art](https://stikka.art)
        - Repo: [GitHub](https://github.com/morgulbrut/stikka-factory)
        - Social: [@stikka](https://chaos.social/@stikka)
        """
    )
    st.subheader("Credits")
    st.markdown(
        """
        - Originally developed by [TAMI](https://telavivmakers.org)
        - Brought to 39C3 by [SGMK](https://mechatronicart.ch/)
        - AI slop by `FLUX1-schnell` on an Nvidia `L40S` at [cloudscale](https://cloudscale.ch)
        """
    )
    st.subheader("FAQ")
    st.markdown(
        """
        - *Dithering* is suggested (sometimes inforced) if source is not lineart as grayscale and color look bad at thermal printer
        - Switching tabs doesn't re-detect printers, refreshing the page and buttons will do it
        - To save the generated images on the host, set `privacy_mode = false` in `config.toml`
        - To disable printer sleep mode:
            1. Discover the printer with `brother_ql discover`, it returns something like `Found compatible printer QL-600 at: usb://0x04f9:0x20c0/000H2G258173` were `usb://0x04f9:0x20c0/000H2G258173` is the printer id
            2. Set the `power-off-delay` to 0: `brother_ql -p <PRINTER ID> configure set power-off-delay 0`. You can check the set value `brother_ql -p <PRINTER ID> configure get power-off-delay`

        PRINT ALOT is the best!
        """
    )
    st.image(Image.open("assets/stikka_39c3.jpg"), caption="39C3 Stikka Centraal", width='stretch')
