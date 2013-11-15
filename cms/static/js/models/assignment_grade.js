define(["backbone", "underscore"], function(Backbone, _) {
    var AssignmentGrade = Backbone.Model.extend({
        defaults : {
            graderType : null, // the type label (string). May be "Not Graded" which implies None. 
            locator : null // locator for the block
        },
        idAttribute: 'locator',
        urlRoot : '/xblock/',
        url: function() {
            // add ?filter=graderType to the request url
            return Backbone.Model.prototype.url.apply(this) + '?' + $.param({filter: 'graderType'});
        }
    });
    return AssignmentGrade;
});
