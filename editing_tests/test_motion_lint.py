"""Anti-slideshow lint: opacity-only sections are counted from the composition's own GSAP and CSS."""
from prosperos_hoard.editing.motion_lint import slideshow_lint

SLIDES = """<style>.s{position:absolute}</style><script>
const tl = gsap.timeline({paused:true});
tl.to('#s1', {opacity: 1, duration: .4}).to('#s1', {opacity: 0}, 2)
  .fromTo("#s2", {opacity: 0}, {opacity: 1, duration: .5}, 2.2)
  .to(`#s3`, {autoAlpha: 1, ease: "power2.out"}, 4);
window.seek = (t) => tl.seek(t);
</script>"""

MOVING = """<script>
const tl = gsap.timeline({paused:true});
tl.from('#s1', {opacity: 0, y: 40}).to('#s2', {opacity: 1, scale: 1.1}).fromTo('#s3', {opacity: 0, x: -200}, {opacity: 1, x: 0})
  .to('.logo', {rotation: 15});
</script>"""


def test_three_fading_sections_are_a_slideshow():
    r = slideshow_lint(SLIDES)
    assert r["verdict"] == "reject" and r["opacity_only"] == ["#s1", "#s2", "#s3"]
    assert "slideshow" in r["reprompt"] and "#s1" in r["reprompt"]


def test_sections_that_move_pass():
    r = slideshow_lint(MOVING)
    assert r["verdict"] == "pass" and r["opacity_only_count"] == 0 and r["moving_count"] == 4


def test_css_keyframes_and_transitions_count_too():
    css = """<style>
    @keyframes fadeIn { from { opacity: 0 } to { opacity: 1 } }
    @keyframes rise { from { transform: translateY(20px); opacity: 0 } to { transform: none; opacity: 1 } }
    .a { animation: fadeIn 1s both } .b { animation-name: fadeIn } .c { transition: opacity .3s ease }
    .d { animation: rise 1s }
    </style>"""
    r = slideshow_lint(css)
    assert r["verdict"] == "reject" and set(r["opacity_only"]) == {".a", ".b", ".c"} and r["moving_count"] == 1
    assert slideshow_lint(css, threshold=4)["verdict"] == "pass"


def test_tweens_with_nested_objects_are_read():
    # stagger objects, scrollTrigger and callbacks nest braces in the vars; those tweens used to be skipped entirely
    nested = """<script>const tl = gsap.timeline();
tl.from('.s1', {opacity: 0, duration: 1, stagger: {each: 0.1, from: "start"}})
  .from(".s2", {autoAlpha: 0, onComplete: () => { done = {x: 1}; }})
  .fromTo('.s3', {opacity: 0}, {opacity: 1, scrollTrigger: {trigger: '.s3', start: "top: 80%"}})
  .from('.s4', {opacity: 0, stagger: {each: 0.2}, y: 30});</script>"""
    r = slideshow_lint(nested)
    assert r["verdict"] == "reject" and r["opacity_only"] == [".s1", ".s2", ".s3"] and r["moving_count"] == 1
