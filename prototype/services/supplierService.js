window.SupplierService = {
  async getAll() { return SUPPLIERS; },
  async getById(id) { return SUPPLIERS.find(s => s.id === id) || null; }
};