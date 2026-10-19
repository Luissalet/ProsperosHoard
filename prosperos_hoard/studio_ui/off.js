document.getElementById('enable').onclick = async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const response = await fetch('/api/studio/modules/'+document.body.dataset.module, {
      method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:true,automatic:false})});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || response.statusText);
    location.reload();
  } catch (error) {
    document.getElementById('error').textContent = error.message;
    button.disabled = false;
  }
};
