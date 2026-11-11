// Replace this mock implementation with API calls when providers are selected.
window.ProductService = {
  async searchProducts(query = "", category = "") {
    await new Promise(resolve => setTimeout(resolve, 250)); // simulate network latency
    const q = query.trim().toLowerCase();
    return PRODUCTS.filter(p => {
      const supplier = SUPPLIERS.find(s => s.id === p.supplierId);
      const haystack = [p.name,p.category,p.description,...Object.values(p.specifications),supplier?.name].join(" ").toLowerCase();
      return (!q || haystack.includes(q)) && (!category || p.category === category);
    });
  },
  async getProductDetails(productId) { return PRODUCTS.find(p => p.id === productId) || null; },
  async getSuppliers() { return SUPPLIERS; },
  async getProductsBySupplier(supplierId) { return PRODUCTS.filter(p => p.supplierId === supplierId); },
  normalizeProduct(raw) {
    // Map external provider fields into this application's standard product shape.
    return {id:String(raw.id || crypto.randomUUID()),name:raw.name || raw.title || "Unnamed product",
      category:raw.category || "Other",description:raw.description || "",specifications:raw.specifications || {},
      price:Number(raw.price || 0),currency:raw.currency || "KES",supplierId:raw.supplierId || "",
      sourceUrl:raw.sourceUrl || raw.url || "",retrievedAt:new Date().toISOString().slice(0,10),isMock:false};
  }
};